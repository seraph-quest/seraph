"""First-pass explicit human/world model layered on top of guardian state."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.observer.context import CurrentContext

_TAG_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


@dataclass(frozen=True)
class GuardianWorldModel:
    current_focus: str
    active_commitments: tuple[str, ...]
    open_loops_or_pressure: tuple[str, ...]
    focus_alignment: str
    intervention_receptivity: str
    active_projects: tuple[str, ...] = ()
    execution_pressure: tuple[str, ...] = ()
    active_constraints: tuple[str, ...] = ()
    recurring_patterns: tuple[str, ...] = ()
    active_routines: tuple[str, ...] = ()
    project_state: tuple[str, ...] = ()
    memory_signals: tuple[str, ...] = ()
    continuity_threads: tuple[str, ...] = ()
    collaborators: tuple[str, ...] = ()
    recurring_obligations: tuple[str, ...] = ()
    project_timeline: tuple[str, ...] = ()

    def to_prompt_block(self) -> str:
        lines = [
            f"Current focus: {self.current_focus}",
            f"Focus alignment: {self.focus_alignment}",
            f"Intervention receptivity: {self.intervention_receptivity}",
        ]

        if self.active_commitments:
            lines.append("Active commitments:")
            lines.extend(f"- {item}" for item in self.active_commitments)

        if self.active_projects:
            lines.append("Active projects:")
            lines.extend(f"- {item}" for item in self.active_projects)

        if self.project_state:
            lines.append("Project state:")
            lines.extend(f"- {item}" for item in self.project_state)

        if self.memory_signals:
            lines.append("Memory signals:")
            lines.extend(f"- {item}" for item in self.memory_signals)

        if self.continuity_threads:
            lines.append("Continuity threads:")
            lines.extend(f"- {item}" for item in self.continuity_threads)

        if self.collaborators:
            lines.append("Collaborators:")
            lines.extend(f"- {item}" for item in self.collaborators)

        if self.recurring_obligations:
            lines.append("Recurring obligations:")
            lines.extend(f"- {item}" for item in self.recurring_obligations)

        if self.project_timeline:
            lines.append("Project timeline:")
            lines.extend(f"- {item}" for item in self.project_timeline)

        if self.open_loops_or_pressure:
            lines.append("Open loops and pressure:")
            lines.extend(f"- {item}" for item in self.open_loops_or_pressure)

        if self.execution_pressure:
            lines.append("Recent execution pressure:")
            lines.extend(f"- {item}" for item in self.execution_pressure)

        if self.active_constraints:
            lines.append("Active constraints:")
            lines.extend(f"- {item}" for item in self.active_constraints)

        if self.recurring_patterns:
            lines.append("Recurring patterns:")
            lines.extend(f"- {item}" for item in self.recurring_patterns)

        if self.active_routines:
            lines.append("Active routines:")
            lines.extend(f"- {item}" for item in self.active_routines)

        return "\n".join(lines)


def _clean_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    if line.startswith("- "):
        line = line[2:].strip()
    line = _TAG_PREFIX_RE.sub("", line)
    return " ".join(line.split())


def _extract_lines(block: str, *, limit: int = 2) -> list[str]:
    results: list[str] = []
    for raw_line in block.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        results.append(line)
        if len(results) >= limit:
            break
    return results


def _extract_tagged_memory(block: str, tag: str, *, limit: int = 2) -> list[str]:
    prefix = f"[{tag}]"
    results: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if not line.lower().startswith(prefix):
            continue
        cleaned = _clean_line(line)
        if cleaned:
            results.append(cleaned)
        if len(results) >= limit:
            break
    return results


def _dedupe(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def _derive_focus_alignment(observer_context: CurrentContext) -> str:
    if observer_context.salience_reason in {
        "current_event",
        "upcoming_event",
        "aligned_work_activity",
    }:
        return "high"
    if observer_context.salience_reason in {"active_goals", "recent_git_activity", "screen_activity"}:
        return "medium"
    return "low"


def _derive_intervention_receptivity(observer_context: CurrentContext) -> str:
    if (
        observer_context.interruption_mode == "focus"
        or observer_context.user_state in {"deep_work", "in_meeting", "away"}
        or observer_context.interruption_cost == "high"
        or observer_context.attention_budget_remaining <= 1
    ):
        return "low"
    if (
        observer_context.user_state == "winding_down"
        or observer_context.interruption_cost == "medium"
        or observer_context.observer_confidence != "grounded"
    ):
        return "medium"
    return "high"


def build_guardian_world_model(
    *,
    observer_context: CurrentContext,
    memory_context: str,
    current_session_history: str,
    recent_sessions_summary: str,
    recent_intervention_feedback: str,
    active_projects: tuple[str, ...] = (),
    recent_execution_summary: str = "",
) -> GuardianWorldModel:
    """Build a first explicit working-state / commitments model from current signals."""
    memory_lines = _extract_lines(memory_context, limit=3)
    recent_session_lines = _extract_lines(recent_sessions_summary, limit=2)
    feedback_lines = _extract_lines(recent_intervention_feedback, limit=2)
    goal_memory = _extract_tagged_memory(memory_context, "goal", limit=2)
    preference_constraints = _extract_tagged_memory(memory_context, "preference", limit=2)
    recurring_patterns = _extract_tagged_memory(memory_context, "pattern", limit=3)
    active_routines = _extract_tagged_memory(memory_context, "routine", limit=3)
    collaborators = _extract_tagged_memory(memory_context, "collaborator", limit=3)
    recurring_obligations = _extract_tagged_memory(memory_context, "obligation", limit=3)
    project_timeline = _dedupe(
        _extract_tagged_memory(memory_context, "timeline", limit=3)
        + list(active_projects[:1])
        + _extract_lines(recent_execution_summary, limit=2)
        + recent_session_lines[:1]
    )
    memory_signals = _dedupe(goal_memory + recurring_patterns[:1] + preference_constraints[:1] + active_routines[:1])
    continuity_threads = _dedupe(recent_session_lines + feedback_lines[:1])

    current_focus = "No clear focus signal"
    if observer_context.current_event:
        current_focus = observer_context.current_event
    elif observer_context.active_goals_summary and observer_context.active_window:
        current_focus = f"{observer_context.active_goals_summary} while in {observer_context.active_window}"
    elif observer_context.active_goals_summary:
        current_focus = observer_context.active_goals_summary
    elif observer_context.active_window:
        current_focus = observer_context.active_window
    elif observer_context.screen_context:
        current_focus = observer_context.screen_context.strip().splitlines()[0][:120]
    elif current_session_history.strip():
        current_focus = _extract_lines(current_session_history, limit=1)[0]
    elif goal_memory:
        current_focus = goal_memory[0]
    elif recent_session_lines:
        current_focus = recent_session_lines[0]

    commitments: list[str] = []
    if observer_context.current_event:
        commitments.append(observer_context.current_event)
    for event in observer_context.upcoming_events[:2]:
        summary = str(event.get("summary") or "").strip()
        if summary:
            commitments.append(summary)
    if observer_context.active_goals_summary:
        commitments.append(observer_context.active_goals_summary)
    commitments.extend(memory_lines[:1])
    commitments.extend(active_projects[:2])

    open_loops: list[str] = []
    if observer_context.attention_budget_remaining <= 1:
        open_loops.append("Attention budget is nearly exhausted")
    if observer_context.user_state in {"deep_work", "in_meeting", "away", "winding_down"}:
        open_loops.append(f"Current state: {observer_context.user_state}")
    if "not_helpful" in recent_intervention_feedback or "failed" in recent_intervention_feedback:
        open_loops.append("Recent intervention friction is present")
    if recent_session_lines:
        open_loops.append(recent_session_lines[0])
    for line in memory_lines[1:]:
        open_loops.append(line)
    execution_pressure = _extract_lines(recent_execution_summary, limit=3)
    active_constraints: list[str] = []
    if observer_context.interruption_mode == "focus":
        active_constraints.append("User is in focus mode")
    if observer_context.attention_budget_remaining <= 1:
        active_constraints.append("Attention budget is nearly exhausted")
    if observer_context.user_state in {"deep_work", "in_meeting", "away"}:
        active_constraints.append(f"Current state is {observer_context.user_state}")
    active_constraints.extend(preference_constraints)
    project_state = list(active_projects[:3])
    project_state.extend(execution_pressure[:2])

    return GuardianWorldModel(
        current_focus=current_focus,
        active_commitments=_dedupe(commitments),
        open_loops_or_pressure=_dedupe(open_loops),
        focus_alignment=_derive_focus_alignment(observer_context),
        intervention_receptivity=_derive_intervention_receptivity(observer_context),
        active_projects=_dedupe(list(active_projects)),
        execution_pressure=_dedupe(execution_pressure),
        active_constraints=_dedupe(active_constraints),
        recurring_patterns=_dedupe(recurring_patterns),
        active_routines=_dedupe(active_routines),
        project_state=_dedupe(project_state),
        memory_signals=memory_signals,
        continuity_threads=continuity_threads,
        collaborators=_dedupe(collaborators),
        recurring_obligations=_dedupe(recurring_obligations),
        project_timeline=project_timeline,
    )
