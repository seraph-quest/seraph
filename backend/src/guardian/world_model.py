"""First-pass explicit human/world model layered on top of guardian state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.observer.context import CurrentContext

if TYPE_CHECKING:
    from src.guardian.feedback import GuardianLearningSignal

_TAG_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_ROLE_PREFIX_RE = re.compile(r"^(user|assistant|system):\s*", re.IGNORECASE)


@dataclass(frozen=True)
class GuardianWorldModel:
    current_focus: str
    focus_source: str
    active_commitments: tuple[str, ...]
    open_loops_or_pressure: tuple[str, ...]
    focus_alignment: str
    intervention_receptivity: str
    active_blockers: tuple[str, ...] = ()
    next_up: tuple[str, ...] = ()
    dominant_thread: str = "No dominant thread"
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
    corroboration_sources: tuple[str, ...] = ()
    judgment_risks: tuple[str, ...] = ()

    def to_prompt_block(self) -> str:
        lines = [
            f"Current focus: {self.current_focus}",
            f"Focus source: {self.focus_source.replace('_', ' ')}",
            f"Focus alignment: {self.focus_alignment}",
            f"Intervention receptivity: {self.intervention_receptivity}",
        ]
        if self.corroboration_sources:
            lines.append(f"Corroboration sources: {', '.join(self.corroboration_sources)}")
        if self.judgment_risks:
            lines.append("Judgment risks:")
            lines.extend(f"- {item}" for item in self.judgment_risks)

        if self.active_commitments:
            lines.append("Active commitments:")
            lines.extend(f"- {item}" for item in self.active_commitments)

        if self.active_projects:
            lines.append("Active projects:")
            lines.extend(f"- {item}" for item in self.active_projects)

        if self.active_blockers:
            lines.append("Active blockers:")
            lines.extend(f"- {item}" for item in self.active_blockers)

        if self.next_up:
            lines.append("Next up:")
            lines.extend(f"- {item}" for item in self.next_up)

        lines.append(f"Dominant thread: {self.dominant_thread}")

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


def _extract_session_focus(block: str) -> str:
    for raw_line in reversed(block.splitlines()):
        line = _clean_line(raw_line)
        if not line:
            continue
        return _ROLE_PREFIX_RE.sub("", line).strip()
    return ""


def _dedupe(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def _prioritize_topic_context(items: list[str], topic: str | None, *, limit: int | None = None) -> list[str]:
    if not topic:
        prioritized = list(_dedupe(items))
    else:
        matching = [item for item in items if _text_matches_topic(item, topic)]
        non_matching = [item for item in items if not _text_matches_topic(item, topic)]
        prioritized = list(_dedupe(matching + non_matching))
    if limit is not None:
        return prioritized[:limit]
    return prioritized


def _normalize_topic(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(normalized.split())


def _text_matches_topic(candidate: str, topic: str | None) -> bool:
    normalized_candidate = _normalize_topic(candidate)
    normalized_topic = _normalize_topic(topic)
    if not normalized_candidate or not normalized_topic:
        return False
    return (
        normalized_topic in normalized_candidate
        or normalized_candidate in normalized_topic
    )


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


def _derive_intervention_receptivity(
    observer_context: CurrentContext,
    learning_signal: GuardianLearningSignal | None,
    *,
    has_supporting_context_conflict: bool = False,
    has_execution_context_conflict: bool = False,
) -> str:
    negative_outcomes = 0
    positive_outcomes = 0
    if learning_signal is not None:
        negative_outcomes = int(
            learning_signal.not_helpful_count + learning_signal.failed_count
        )
        positive_outcomes = int(
            learning_signal.helpful_count + learning_signal.acknowledged_count
        )
    if (
        observer_context.interruption_mode == "focus"
        or observer_context.user_state in {"deep_work", "in_meeting", "away"}
        or observer_context.interruption_cost == "high"
        or observer_context.attention_budget_remaining <= 1
    ):
        if (
            learning_signal is not None
            and learning_signal.blocked_state_bias == "prefer_async_for_blocked_state"
            and learning_signal.channel_bias == "prefer_native_notification"
        ):
            return "guarded_async"
        return "low"
    if (
        learning_signal is not None
        and negative_outcomes >= 2
        and negative_outcomes > positive_outcomes
        and (has_supporting_context_conflict or has_execution_context_conflict)
    ):
        return "low"
    if (
        learning_signal is not None
        and negative_outcomes >= 2
        and negative_outcomes > positive_outcomes
    ):
        return "medium"
    if (
        learning_signal is not None
        and learning_signal.timing_bias == "prefer_available_windows"
        and observer_context.user_state == "available"
        and observer_context.interruption_cost != "high"
    ):
        return "high"
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
    memory_buckets: dict[str, tuple[str, ...]] | None = None,
    learning_signal: GuardianLearningSignal | None = None,
) -> GuardianWorldModel:
    """Build a first explicit working-state / commitments model from current signals."""
    memory_lines = _extract_lines(memory_context, limit=3)
    recent_session_lines = _extract_lines(recent_sessions_summary, limit=2)
    feedback_lines = _extract_lines(recent_intervention_feedback, limit=2)
    memory_buckets = memory_buckets or {}
    goal_memory = list(memory_buckets.get("goal", ()))[:2] or _extract_tagged_memory(memory_context, "goal", limit=2)
    commitment_memory = list(memory_buckets.get("commitment", ()))[:2] or _extract_tagged_memory(memory_context, "commitment", limit=2)
    preference_constraints = list(memory_buckets.get("preference", ()))[:2] or _extract_tagged_memory(memory_context, "preference", limit=2)
    procedural_constraints = list(memory_buckets.get("procedural", ()))[:4] or _extract_tagged_memory(memory_context, "procedural", limit=4)
    recurring_patterns = list(memory_buckets.get("pattern", ()))[:3] or _extract_tagged_memory(memory_context, "pattern", limit=3)
    project_memory = list(memory_buckets.get("project", ()))[:2] or _extract_tagged_memory(memory_context, "project", limit=2)
    active_routines = list(memory_buckets.get("routine", ()))[:3] or _extract_tagged_memory(memory_context, "routine", limit=3)
    collaborators = list(memory_buckets.get("collaborator", ()))[:3] or _extract_tagged_memory(memory_context, "collaborator", limit=3)
    recurring_obligations = list(memory_buckets.get("obligation", ()))[:3] or _extract_tagged_memory(memory_context, "obligation", limit=3)
    timeline_memory = list(memory_buckets.get("timeline", ()))[:3] or _extract_tagged_memory(memory_context, "timeline", limit=3)
    current_focus = "No clear focus signal"
    focus_source = "unknown"
    if observer_context.current_event:
        current_focus = observer_context.current_event
        focus_source = "current_event"
    elif observer_context.active_goals_summary and observer_context.active_window:
        current_focus = f"{observer_context.active_goals_summary} while in {observer_context.active_window}"
        focus_source = "observer_goal_window"
    elif observer_context.active_goals_summary:
        current_focus = observer_context.active_goals_summary
        focus_source = "observer_goals"
    elif observer_context.active_window:
        current_focus = observer_context.active_window
        focus_source = "observer_window"
    elif observer_context.screen_context:
        current_focus = observer_context.screen_context.strip().splitlines()[0][:120]
        focus_source = "observer_screen"
    elif current_session_history.strip():
        session_focus = _extract_session_focus(current_session_history)
        if session_focus:
            current_focus = session_focus
            focus_source = "current_session"
    elif goal_memory:
        current_focus = goal_memory[0]
        focus_source = "memory"
    elif recent_session_lines:
        current_focus = recent_session_lines[0]
        focus_source = "recent_sessions"

    observer_project = _clean_line(observer_context.active_project or "")
    live_topic_anchor = (
        observer_project
        or _clean_line(observer_context.current_event or "")
        or _clean_line(observer_context.active_goals_summary or "")
    )
    recent_session_lines = _prioritize_topic_context(recent_session_lines, live_topic_anchor, limit=2)
    project_memory = _prioritize_topic_context(project_memory, live_topic_anchor, limit=2)
    active_routines = _prioritize_topic_context(active_routines, live_topic_anchor, limit=3)
    collaborators = _prioritize_topic_context(collaborators, live_topic_anchor, limit=3)
    recurring_obligations = _prioritize_topic_context(recurring_obligations, live_topic_anchor, limit=3)
    timeline_memory = _prioritize_topic_context(timeline_memory, live_topic_anchor, limit=3)
    project_timeline = _dedupe(
        timeline_memory
        + project_memory
        + list(active_projects[:1])
        + _extract_lines(recent_execution_summary, limit=2)
        + recent_session_lines[:1]
    )
    memory_signals = _dedupe(
        goal_memory
        + commitment_memory[:1]
        + recurring_patterns[:1]
        + preference_constraints[:1]
        + procedural_constraints[:1]
        + active_routines[:1]
    )
    continuity_threads = _dedupe(recent_session_lines + feedback_lines[:1])
    execution_pressure = _extract_lines(recent_execution_summary, limit=3)
    matching_recent_threads = [
        item for item in recent_session_lines
        if _text_matches_topic(item, live_topic_anchor)
    ] if live_topic_anchor else []
    stale_recent_threads = [
        item for item in recent_session_lines
        if not _text_matches_topic(item, live_topic_anchor)
    ] if live_topic_anchor else []

    commitments: list[str] = []
    if observer_context.current_event:
        commitments.append(observer_context.current_event)
    for event in observer_context.upcoming_events[:2]:
        summary = str(event.get("summary") or "").strip()
        if summary:
            commitments.append(summary)
    if observer_context.active_goals_summary:
        commitments.append(observer_context.active_goals_summary)
    commitments.extend(matching_recent_threads[:1])
    commitments.extend(commitment_memory[:2])
    commitments.extend(project_memory[:1])
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
    open_loops.extend(execution_pressure[:1])
    for line in memory_lines[1:]:
        open_loops.append(line)
    active_blockers = _dedupe(
        [
            line for line in open_loops[:2]
            if "Current state:" not in line and "Attention budget" not in line
        ]
        + list(execution_pressure[:1])
        + list(preference_constraints[:1])
        + list(procedural_constraints[:1])
    )
    next_up = _dedupe(
        matching_recent_threads[:1]
        + list(commitment_memory[:1])
        + project_memory[:1]
        + list(active_projects[:1])
        + list(goal_memory[:1])
        + list(recent_session_lines[:1])
    )
    dominant_thread = (
        matching_recent_threads[0]
        if matching_recent_threads
        else (continuity_threads[0]
        if continuity_threads
        else (active_projects[0] if active_projects else current_focus)
        )
    )
    active_constraints: list[str] = []
    if observer_context.interruption_mode == "focus":
        active_constraints.append("User is in focus mode")
    if observer_context.attention_budget_remaining <= 1:
        active_constraints.append("Attention budget is nearly exhausted")
    if observer_context.user_state in {"deep_work", "in_meeting", "away"}:
        active_constraints.append(f"Current state is {observer_context.user_state}")
    active_constraints.extend(preference_constraints)
    active_constraints.extend(procedural_constraints)
    durable_project_signals = _dedupe(project_memory[:2])
    recent_project_signals = _dedupe(list(active_projects[:3]))
    active_project_signals = _dedupe(
        ([observer_project] if observer_project else [])
        + list(durable_project_signals)
        + list(recent_project_signals)
    )
    project_state = _dedupe(list(active_project_signals) + matching_recent_threads[:1] + execution_pressure[:2])
    has_observer_focus_signal = any(
        (
            observer_context.current_event,
            observer_context.active_goals_summary,
            observer_context.active_window,
            observer_context.screen_context,
        )
    )

    corroboration_sources: list[str] = []
    if has_observer_focus_signal:
        corroboration_sources.append("observer")
    if goal_memory or memory_lines:
        corroboration_sources.append("memory")
    if current_session_history.strip():
        corroboration_sources.append("current_session")
    if recent_session_lines:
        corroboration_sources.append("recent_sessions")
    if active_projects or project_memory:
        corroboration_sources.append("projects")
    if execution_pressure:
        corroboration_sources.append("execution")

    judgment_risks: list[str] = []
    if focus_source in {"current_session", "memory", "recent_sessions"}:
        judgment_risks.append(
            f"Current focus is inferred from {focus_source.replace('_', ' ')} instead of live observer signals."
        )
    if (
        observer_project
        and durable_project_signals
        and not any(_text_matches_topic(item, observer_project) for item in durable_project_signals)
    ):
        judgment_risks.append(
            f"Live observer project '{observer_project}' does not match recalled project context."
        )
    supporting_context = list(collaborators) + list(recurring_obligations) + list(timeline_memory)
    matching_support = [
        item for item in supporting_context
        if _text_matches_topic(item, observer_project)
    ] if observer_project else []
    stale_support = [
        item for item in supporting_context
        if not _text_matches_topic(item, observer_project)
    ] if observer_project else []
    matching_execution_pressure = [
        item for item in execution_pressure
        if _text_matches_topic(item, observer_project)
    ] if observer_project else []
    stale_execution_pressure = [
        item for item in execution_pressure
        if not _text_matches_topic(item, observer_project)
    ] if observer_project else []
    has_follow_through_pressure = bool(
        observer_project and matching_recent_threads and matching_execution_pressure
    )
    has_supporting_context_conflict = bool(observer_project and stale_support)
    has_execution_context_conflict = bool(observer_project and stale_execution_pressure)
    if observer_project and len(stale_support) >= 2 and not matching_support:
        judgment_risks.append(
            f"Recalled collaborator, obligation, or timeline context does not support live project '{observer_project}'."
        )
    elif observer_project and matching_support and stale_support:
        judgment_risks.append(
            f"Some recalled collaborator, obligation, or timeline context still points away from live project '{observer_project}'."
        )
    if observer_project and stale_execution_pressure and not matching_execution_pressure:
        judgment_risks.append(
            f"Recent execution pressure does not line up with live project '{observer_project}'."
        )
    elif observer_project and matching_execution_pressure and stale_execution_pressure:
        judgment_risks.append(
            f"Some recent execution pressure still points away from live project '{observer_project}'."
        )
    if observer_project and stale_recent_threads and not matching_recent_threads:
        judgment_risks.append(
            f"Recent cross-thread continuity does not support live project '{observer_project}'."
        )
    elif observer_project and matching_recent_threads and stale_recent_threads:
        judgment_risks.append(
            f"Some recent cross-thread continuity still points away from live project '{observer_project}'."
        )
    if has_follow_through_pressure:
        judgment_risks.append(
            f"Cross-thread commitments and recent execution setbacks suggest live follow-through risk on project '{observer_project}'."
        )
        open_loops.append(
            f"Follow-through risk remains open for live project '{observer_project}'."
        )
        active_blockers = _dedupe(
            list(active_blockers)
            + [f"Follow-through risk remains open for live project '{observer_project}'"]
        )
    has_live_focus_anchor = focus_source == "current_event" or focus_source.startswith("observer_")
    if (
        not observer_project
        and len(active_project_signals) >= 2
        and not has_live_focus_anchor
    ):
        judgment_risks.append(
            "Multiple active projects are competing without a live observer project anchor."
        )
    if (
        learning_signal is not None
        and (learning_signal.not_helpful_count + learning_signal.failed_count) >= 2
        and (learning_signal.not_helpful_count + learning_signal.failed_count)
        > (learning_signal.helpful_count + learning_signal.acknowledged_count)
    ):
        judgment_risks.append(
            "Recent intervention outcomes skew negative, so the guardian should stay selective."
        )
        if execution_pressure:
            judgment_risks.append(
                "Recent execution setbacks and intervention misses suggest follow-through risk."
            )

    return GuardianWorldModel(
        current_focus=current_focus,
        focus_source=focus_source,
        active_commitments=_dedupe(commitments),
        open_loops_or_pressure=_dedupe(open_loops),
        focus_alignment=_derive_focus_alignment(observer_context),
        intervention_receptivity=_derive_intervention_receptivity(
            observer_context,
            learning_signal,
            has_supporting_context_conflict=has_supporting_context_conflict,
            has_execution_context_conflict=has_execution_context_conflict,
        ),
        active_blockers=active_blockers,
        next_up=next_up,
        dominant_thread=dominant_thread,
        active_projects=active_project_signals,
        execution_pressure=_dedupe(execution_pressure),
        active_constraints=_dedupe(active_constraints),
        recurring_patterns=_dedupe(recurring_patterns),
        active_routines=_dedupe(active_routines),
        project_state=project_state,
        memory_signals=memory_signals,
        continuity_threads=continuity_threads,
        collaborators=_dedupe(collaborators),
        recurring_obligations=_dedupe(recurring_obligations),
        project_timeline=project_timeline,
        corroboration_sources=_dedupe(corroboration_sources),
        judgment_risks=_dedupe(judgment_risks),
    )
