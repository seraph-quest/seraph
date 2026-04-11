"""Explicit guardian-state synthesis for downstream agent and scheduler paths."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.agent.session import session_manager
from src.guardian.world_model import GuardianWorldModel, build_guardian_world_model
from src.guardian.learning_arbitration import GuardianLearningArbitration
from src.guardian.feedback import ScopedGuardianLearningResolution
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
    episodic_memory_context: str
    current_session_history: str
    recent_sessions_summary: str
    recent_intervention_feedback: str
    confidence: GuardianStateConfidence
    recent_execution_summary: str = ""
    learning_guidance: str = ""
    learning_diagnostics: tuple[str, ...] = ()
    bounded_memory_context: str = ""
    memory_benchmark_diagnostics: tuple[str, ...] = ()
    memory_provider_diagnostics: tuple[str, ...] = ()
    memory_reconciliation_diagnostics: tuple[str, ...] = ()
    intent_uncertainty_level: str = "clear"
    intent_resolution: str = "proceed"
    intent_uncertainty_diagnostics: tuple[str, ...] = ()
    judgment_proof_lines: tuple[str, ...] = ()

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

        if self.bounded_memory_context:
            lines.extend(["", "Bounded recall:", self.bounded_memory_context])

        if self.memory_context:
            lines.extend(["", "Relevant memories:", self.memory_context])

        if self.episodic_memory_context:
            lines.extend(["", "Relevant episodes:", self.episodic_memory_context])

        if self.recent_sessions_summary:
            lines.extend(["", "Recent sessions:", self.recent_sessions_summary])

        if self.recent_intervention_feedback:
            lines.extend(["", "Recent intervention feedback:", self.recent_intervention_feedback])

        if self.learning_guidance:
            lines.extend(["", "Learned communication guidance:", self.learning_guidance])

        if self.learning_diagnostics:
            lines.append("")
            lines.append("Learning diagnostics:")
            lines.extend(f"- {item}" for item in self.learning_diagnostics)

        if self.memory_benchmark_diagnostics:
            lines.append("")
            lines.append("Memory benchmark diagnostics:")
            lines.extend(f"- {item}" for item in self.memory_benchmark_diagnostics)

        if self.memory_provider_diagnostics:
            lines.append("")
            lines.append("Memory provider diagnostics:")
            lines.extend(f"- {item}" for item in self.memory_provider_diagnostics)

        if self.memory_reconciliation_diagnostics:
            lines.append("")
            lines.append("Memory reconciliation diagnostics:")
            lines.extend(f"- {item}" for item in self.memory_reconciliation_diagnostics)

        if self.intent_uncertainty_diagnostics:
            lines.append("")
            lines.append(
                f"Intent uncertainty: {self.intent_uncertainty_level} (recommended resolution: {self.intent_resolution})"
            )
            lines.extend(f"- {item}" for item in self.intent_uncertainty_diagnostics)

        if self.judgment_proof_lines:
            lines.append("")
            lines.append("Judgment proof:")
            lines.extend(f"- {item}" for item in self.judgment_proof_lines)

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
    if observer_confidence == "degraded":
        return "degraded"
    if world_model_status == "degraded":
        return "partial" if observer_confidence == "grounded" else "degraded"
    degraded_signals = sum(
        1
        for status in (
            world_model_status,
            memory_status,
            current_session_status,
            recent_sessions_status,
        )
        if status == "degraded"
    )
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
    if observer_confidence == "partial":
        return "partial" if grounded_signals or degraded_signals else "degraded"
    if degraded_signals:
        return "partial"
    if world_model_status == "partial":
        return "partial"
    if grounded_signals >= 2 and world_model_status == "grounded":
        return "grounded"
    return "partial"


def _world_model_status(world_model: GuardianWorldModel) -> str:
    if any(
        "does not match recalled project context" in item
        or "does not support live project" in item
        or "does not line up with live project" in item
        for item in world_model.judgment_risks
    ):
        return "degraded"
    if world_model.judgment_risks:
        return "partial"
    if (
        world_model.current_focus != "No clear focus signal"
        and world_model.active_commitments
        and world_model.dominant_thread != "No dominant thread"
        and len(world_model.corroboration_sources) >= 2
    ):
        return "grounded"
    if (
        world_model.current_focus != "No clear focus signal"
        or world_model.active_commitments
        or world_model.open_loops_or_pressure
        or world_model.active_blockers
        or world_model.next_up
    ):
        return "partial"
    return "empty"


def _learning_guidance_text(
    *,
    delivery_bias: str,
    phrasing_bias: str,
    cadence_bias: str,
    channel_bias: str,
    escalation_bias: str,
    timing_bias: str,
    blocked_state_bias: str,
    suppression_bias: str,
    thread_preference_bias: str,
) -> str:
    guidance: list[str] = []
    if delivery_bias == "reduce_interruptions":
        guidance.append("After recent negative or failed outcomes, reduce direct interruptions.")
    elif delivery_bias == "prefer_direct_delivery":
        guidance.append("When the user is explicitly available, direct delivery is usually tolerated.")

    if phrasing_bias == "be_brief_and_literal":
        guidance.append("Prefer brief, literal wording over flourish when interrupting.")
    elif phrasing_bias == "be_more_direct":
        guidance.append("Prefer direct wording when a nudge is worth sending.")

    if cadence_bias == "bundle_more":
        guidance.append("Prefer bundling lower-urgency nudges instead of interrupting immediately.")
    elif cadence_bias == "check_in_sooner":
        guidance.append("If confidence is good, allow slightly faster check-ins on aligned work.")

    if channel_bias == "prefer_native_notification":
        guidance.append("Async native delivery is usually tolerated better than browser interruption.")

    if escalation_bias == "prefer_async_native":
        guidance.append("Escalate through async native continuation before direct interruption when possible.")

    if timing_bias == "avoid_focus_windows":
        guidance.append("Avoid direct interruptions during deep-work, meeting, or away windows unless urgency is high.")
    elif timing_bias == "prefer_available_windows":
        guidance.append("When possible, deliver nudges while the user is explicitly available.")

    if blocked_state_bias == "avoid_blocked_state_interruptions":
        guidance.append("When the user is blocked, prefer bundling over direct interruption.")
    elif blocked_state_bias == "prefer_async_for_blocked_state":
        guidance.append("When the user is blocked, prefer async native continuation instead of browser interruption.")

    if suppression_bias == "extend_suppression":
        guidance.append("After failed or poorly received nudges, extend quiet periods before trying again.")
    elif suppression_bias == "resume_faster":
        guidance.append("Helpful nudges can shorten the wait before the next aligned follow-up.")

    if thread_preference_bias == "prefer_existing_thread":
        guidance.append("When following up, prefer the existing thread instead of creating a fresh one.")
    elif thread_preference_bias == "prefer_clean_thread":
        guidance.append("After repeated failures, prefer a clean thread or explicit reset before retrying.")

    return "\n".join(f"- {line}" for line in guidance)


def _learning_diagnostics_lines(
    *,
    live_learning_resolution: ScopedGuardianLearningResolution,
    arbitration: GuardianLearningArbitration,
    active_project: str | None,
) -> tuple[str, ...]:
    signal = arbitration.effective_signal
    lines: list[str] = []
    scope_label = live_learning_resolution.dominant_scope.replace("_", "+")
    if live_learning_resolution.dominant_scope in {"project", "thread_project"} and active_project:
        lines.append(
            f"Live learning is currently anchored to {scope_label} scope for '{active_project}'."
        )
    else:
        lines.append(f"Live learning is currently anchored to {scope_label} scope.")
    lines.append(
        "Observed outcomes: "
        f"helpful={signal.helpful_count}, acknowledged={signal.acknowledged_count}, "
        f"not_helpful={signal.not_helpful_count}, failed={signal.failed_count}."
    )
    if any(
        (
            signal.multi_day_positive_days,
            signal.multi_day_negative_days,
            signal.scheduled_positive_days,
            signal.scheduled_negative_days,
        )
    ):
        lines.append(
            "Long-horizon spread: "
            f"multi-day +{signal.multi_day_positive_days}/-{signal.multi_day_negative_days}, "
            f"scheduled +{signal.scheduled_positive_days}/-{signal.scheduled_negative_days}."
        )
    if (
        signal.multi_day_negative_days >= 3
        and signal.multi_day_negative_days > signal.multi_day_positive_days
    ):
        lines.append(
            "Long-horizon policy currently favors abstaining from low-urgency guidance until outcomes recover."
        )
    if (
        signal.scheduled_negative_days >= 2
        and signal.scheduled_negative_days > signal.scheduled_positive_days
    ):
        lines.append(
            "Scheduled policy currently favors deferring routine guidance until review outcomes stabilize."
        )
    selected_biases = [
        f"{decision.axis}={decision.selected_bias}"
        for decision in live_learning_resolution.decisions
        if decision.selected_bias != "neutral"
    ]
    if selected_biases:
        lines.append(f"Dominant live biases: {', '.join(selected_biases[:4])}.")
    procedural_overrides = [
        f"{decision.axis}={decision.selected_bias}"
        for decision in arbitration.decisions
        if decision.selected_source == "procedural_memory" and decision.selected_bias != "neutral"
    ]
    if procedural_overrides:
        lines.append(
            f"Procedural memory is still steering: {', '.join(procedural_overrides[:4])}."
        )
    conflicting_axes = [
        (
            f"{decision.axis}(live={decision.live_bias}, "
            f"procedural={decision.procedural_bias}, "
            f"winner={decision.selected_source})"
        )
        for decision in arbitration.conflicting_decisions()
    ]
    if conflicting_axes:
        lines.append(
            f"Conflicting live vs procedural biases: {', '.join(conflicting_axes[:4])}."
        )
    procedural_override_axes = [
        decision.axis for decision in arbitration.procedural_override_conflicts()
    ]
    if procedural_override_axes:
        lines.append(
            "Governed adaptation should stay review-first because procedural memory is "
            f"overriding live outcomes on {', '.join(procedural_override_axes[:4])}."
        )
    live_override_axes = [
        decision.axis for decision in arbitration.live_override_conflicts()
    ]
    if live_override_axes:
        lines.append(
            "Fresh live outcomes are overruling older procedural guidance on "
            f"{', '.join(live_override_axes[:4])}."
        )
    return tuple(lines)


def _extract_soul_section_lines(soul_context: str, section: str, *, limit: int) -> tuple[str, ...]:
    header = f"## {section}".lower()
    lines = soul_context.splitlines()
    in_section = False
    extracted: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.lower().startswith("## "):
            if line.lower() == header:
                in_section = True
                continue
            if in_section:
                break
        if not in_section or not line:
            continue
        if line.startswith("("):
            continue
        if line.startswith("- "):
            key, _, value = line[2:].partition(":")
            if _ and not value.strip():
                continue
        extracted.append(line)
        if len(extracted) >= limit:
            break
    return tuple(extracted)


def _summarize_bounded_todos(*, todos: list[dict[str, object]]) -> str:
    open_todos = [
        str(item.get("content", "")).strip()
        for item in todos
        if not item.get("completed") and str(item.get("content", "")).strip()
    ][:4]
    completed_todos = [
        str(item.get("content", "")).strip()
        for item in todos
        if item.get("completed") and str(item.get("content", "")).strip()
    ][:2]

    lines: list[str] = []
    if open_todos:
        lines.append(f"- Open todos: {' | '.join(open_todos)}")
    if completed_todos:
        lines.append(f"- Recently completed: {' | '.join(completed_todos)}")
    return "\n".join(lines)


def _merge_memory_contexts(*contexts: str) -> str:
    lines: list[str] = []
    for context in contexts:
        for raw_line in context.splitlines():
            line = raw_line.strip()
            if not line or line in lines:
                continue
            lines.append(line)
    return "\n".join(lines)


def _memory_provider_diagnostic_lines(
    diagnostics: tuple[dict[str, object], ...],
) -> tuple[str, ...]:
    lines: list[str] = []
    for item in diagnostics:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        capabilities = [
            str(capability)
            for capability in item.get("capabilities_used", [])
            if isinstance(capability, str) and capability.strip()
        ]
        quality_state = str(item.get("quality_state") or "idle")
        canonical_authority = str(item.get("canonical_authority") or "").strip()
        provenance = str(item.get("provenance") or "").strip()
        hit_count = int(item.get("hit_count") or 0)
        stale_hit_count = int(item.get("stale_hit_count") or 0)
        suppressed_irrelevant_hit_count = int(item.get("suppressed_irrelevant_hit_count") or 0)
        topic_matches = [
            str(topic)
            for topic in item.get("topic_matches", [])
            if isinstance(topic, str) and topic.strip()
        ]
        line = (
            f"{name} quality={quality_state}, capabilities="
            f"{', '.join(capabilities) if capabilities else 'none'}, "
            f"hits={hit_count}, "
            f"{f'authority={canonical_authority}, ' if canonical_authority else ''}"
            f"{f'provenance={provenance}, ' if provenance else ''}"
            f"stale_suppressed={stale_hit_count}, "
            f"irrelevant_suppressed={suppressed_irrelevant_hit_count}"
        )
        if topic_matches:
            line += f", topic_matches={', '.join(topic_matches[:3])}"
        lines.append(line)
    return tuple(lines)


def _memory_benchmark_diagnostic_lines(
    diagnostics: tuple[dict[str, object], ...],
) -> tuple[str, ...]:
    lines: list[str] = []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        ranking_policy = str(item.get("ranking_policy") or "").strip()
        suppressed_contradictions = int(item.get("suppressed_contradiction_count") or 0)
        status_filter = str(item.get("status_filter") or "").strip()
        suppression_reasons = [
            str(reason)
            for reason in item.get("suppression_reasons", [])
            if isinstance(reason, str) and reason.strip()
        ]
        line = (
            f"ranking={ranking_policy or 'unknown'}, "
            f"contradictions_suppressed={suppressed_contradictions}, "
            f"{f'status_filter={status_filter}, ' if status_filter else ''}"
            f"reasons={', '.join(suppression_reasons) if suppression_reasons else 'none'}"
        )
        example = next(
            (
                candidate
                for candidate in item.get("suppressed_examples", [])
                if isinstance(candidate, dict)
            ),
            None,
        )
        if example:
            line += (
                f", example={str(example.get('suppressed_text') or '').strip()} -> "
                f"{str(example.get('winner_text') or '').strip()}"
            )
        lines.append(line)
    return tuple(lines)


def _memory_reconciliation_diagnostic_lines(summary: dict[str, object]) -> tuple[str, ...]:
    if not isinstance(summary, dict):
        return ()

    policy = summary.get("policy")
    policy_map = policy if isinstance(policy, dict) else {}
    state = str(summary.get("state") or "steady")
    lines = [
        (
            f"state={state}, active={int(summary.get('active_count') or 0)}, "
            f"superseded={int(summary.get('superseded_count') or 0)}, "
            f"archived={int(summary.get('archived_count') or 0)}, "
            f"contradictions={int(summary.get('contradiction_edge_count') or 0)}"
        )
    ]

    authoritative = str(policy_map.get("authoritative_memory") or "").strip()
    reconciliation = str(policy_map.get("reconciliation_policy") or "").strip()
    forgetting = str(policy_map.get("forgetting_policy") or "").strip()
    if authoritative or reconciliation or forgetting:
        lines.append(
            "policy="
            + ", ".join(
                part
                for part in (
                    f"authoritative={authoritative}" if authoritative else "",
                    f"reconciliation={reconciliation}" if reconciliation else "",
                    f"forgetting={forgetting}" if forgetting else "",
                )
                if part
            )
        )

    if str(summary.get("error") or "").strip():
        lines.append(f"error={summary['error']}")

    recent_conflicts = summary.get("recent_conflicts")
    if isinstance(recent_conflicts, list) and recent_conflicts:
        item = recent_conflicts[0] if isinstance(recent_conflicts[0], dict) else {}
        lines.append(
            "recent_conflict="
            + ", ".join(
                part
                for part in (
                    f"summary={str(item.get('summary') or '').strip()}",
                    f"reason={str(item.get('reason') or 'superseded').strip()}",
                    (
                        f"superseded_by={str(item.get('superseded_by_memory_id')).strip()}"
                        if item.get("superseded_by_memory_id")
                        else ""
                    ),
                )
                if part and not part.endswith("=")
            )
        )

    recent_archivals = summary.get("recent_archivals")
    if isinstance(recent_archivals, list) and recent_archivals:
        item = recent_archivals[0] if isinstance(recent_archivals[0], dict) else {}
        lines.append(
            "recent_archival="
            + ", ".join(
                part
                for part in (
                    f"summary={str(item.get('summary') or '').strip()}",
                    f"reason={str(item.get('reason') or 'archived').strip()}",
                )
                if part and not part.endswith("=")
            )
        )

    return tuple(lines)


def _normalize_free_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _intent_uncertainty_diagnostics(
    *,
    user_message: str | None,
    observer_context: CurrentContext,
    world_model: GuardianWorldModel,
) -> tuple[str, str, tuple[str, ...]]:
    if not str(user_message or "").strip():
        return "clear", "proceed", ()

    diagnostics: list[str] = []
    normalized_message = _normalize_free_text(user_message)
    ambiguous_tokens = {
        "this",
        "that",
        "it",
        "them",
        "those",
        "these",
    }
    message_tokens = set(normalized_message.split())
    known_anchors = [
        observer_context.active_project,
        observer_context.active_goals_summary,
        observer_context.current_event,
        *world_model.active_projects,
    ]
    normalized_anchors = [
        _normalize_free_text(item)
        for item in known_anchors
        if _normalize_free_text(item)
    ]
    has_explicit_anchor = any(
        anchor in normalized_message
        for anchor in normalized_anchors
    )
    has_ambiguous_reference = (
        bool(normalized_message)
        and bool(message_tokens & ambiguous_tokens)
        and not has_explicit_anchor
    )
    project_anchor_ambiguous = any(
        "project-anchor evidence remains ambiguous" in item.lower()
        or "project-anchor evidence remains split" in item.lower()
        for item in world_model.judgment_risks
    )
    competing_project_drift = any(
        "competing project evidence currently favors" in item.lower()
        or "attention is drifting toward" in item.lower()
        for item in world_model.judgment_risks
    )
    split_preference_evidence = any(
        "evidence is split between" in item.lower()
        for item in world_model.preference_inference_diagnostics
    )
    observer_uncertain = observer_context.observer_confidence != "grounded"

    if has_ambiguous_reference and (project_anchor_ambiguous or competing_project_drift):
        diagnostics.append(
            "The request uses an ambiguous referent while project-anchor evidence is split, so the intended target is not grounded."
        )
    elif has_ambiguous_reference and len(world_model.active_projects) > 1:
        diagnostics.append(
            "The request uses an ambiguous referent while multiple active projects remain live in the world model."
        )

    if project_anchor_ambiguous:
        diagnostics.append(
            "Competing project anchors remain close enough that the guardian should not overcommit to one project target."
        )
    elif competing_project_drift:
        diagnostics.append(
            "Recent continuity or execution evidence points toward a competing project, so intent resolution should stay conservative."
        )

    if split_preference_evidence:
        diagnostics.append(
            "Preference evidence is split, so the guardian should explain the uncertainty instead of presenting one interaction style as settled."
        )

    if observer_uncertain:
        diagnostics.append(
            f"Observer confidence is {observer_context.observer_confidence}, which weakens intent resolution from live state."
        )

    if not diagnostics:
        return "clear", "proceed", ()

    if has_ambiguous_reference and (project_anchor_ambiguous or competing_project_drift or len(world_model.active_projects) > 1):
        return "high", "clarify", tuple(diagnostics)
    if observer_uncertain or split_preference_evidence:
        return "medium", "proceed_with_caution", tuple(diagnostics)
    return "medium", "defer_or_clarify", tuple(diagnostics)


def _judgment_proof_lines(
    *,
    observer_context: CurrentContext,
    world_model: GuardianWorldModel,
    intent_uncertainty_diagnostics: tuple[str, ...],
) -> tuple[str, ...]:
    lines: list[str] = []
    lower_risks = [item.lower() for item in world_model.judgment_risks]
    lower_preference_diagnostics = [
        item.lower() for item in world_model.preference_inference_diagnostics
    ]
    lower_intent_diagnostics = [item.lower() for item in intent_uncertainty_diagnostics]

    project_anchor_split = any(
        "project-anchor evidence remains split" in item
        or "project-anchor evidence remains ambiguous" in item
        for item in lower_risks
    )
    competing_project_drift = any(
        "competing project evidence currently favors" in item
        or "attention is drifting toward" in item
        for item in lower_risks
    )
    split_preference_evidence = any(
        "evidence is split between" in item for item in lower_preference_diagnostics
    )
    ambiguous_referent = any("ambiguous referent" in item for item in lower_intent_diagnostics)
    observer_uncertain = observer_context.observer_confidence != "grounded"

    if project_anchor_split and competing_project_drift:
        lines.append(
            "Project-target proof: live observer context conflicts with recalled continuity or execution evidence, so the intended project is not grounded."
        )
    elif project_anchor_split:
        lines.append(
            "Project-target proof: live observer context and recalled project evidence are split, so target selection remains ambiguous."
        )
    elif competing_project_drift:
        lines.append(
            "Project-target proof: continuity or execution evidence is drifting toward a competing project, so target selection should stay conservative."
        )

    if split_preference_evidence:
        lines.append(
            "Interaction-style proof: live and procedural preference evidence disagree, so delivery style should be treated as unsettled."
        )

    if ambiguous_referent:
        lines.append(
            "Referent proof: the user message contains an unresolved referent, so clarification is safer than a confident action."
        )

    if observer_uncertain:
        lines.append(
            f"Observer proof: live observer confidence is {observer_context.observer_confidence}, weakening the evidence for a confident judgment."
        )

    return tuple(lines)


async def build_guardian_state(
    *,
    session_id: str | None = None,
    user_message: str | None = None,
    memory_query: str | None = None,
    intervention_type: str = "advisory",
    refresh_observer: bool = False,
) -> GuardianState:
    """Build one explicit guardian-state object from current repo surfaces."""
    from src.memory.soul import render_soul_text
    from src.memory.decay import summarize_memory_reconciliation_state
    from src.memory.procedural_guidance import load_procedural_memory_guidance
    from src.memory.retrieval_planner import plan_memory_retrieval
    from src.memory.snapshots import (
        get_or_create_bounded_guardian_snapshot,
        render_bounded_guardian_snapshot,
    )
    from src.profile.service import sync_soul_file_to_profile
    from src.audit.repository import audit_repository
    from src.guardian.feedback import guardian_feedback_repository
    from src.guardian.learning_arbitration import arbitrate_learning_signal
    from src.observer.manager import context_manager
    from src.observer.screen_repository import screen_observation_repo

    observer_context = (
        await context_manager.refresh() if refresh_observer else context_manager.get_context()
    )
    normalized_intervention_type = str(intervention_type or "").strip() or "advisory"
    soul_context = render_soul_text(await sync_soul_file_to_profile())
    session_record = await session_manager.get(session_id) if session_id is not None else None

    current_session_history = (
        await session_manager.get_history_text(session_id)
        if session_id is not None
        else ""
    )
    session_todos = await session_manager.get_todos(session_id) if session_id is not None else []
    recent_sessions_summary = await session_manager.get_recent_sessions_summary(
        exclude_session_id=session_id
    )
    live_learning_resolution = await guardian_feedback_repository.resolve_learning_signal(
        intervention_type=normalized_intervention_type,
        limit=12,
        session_id=session_id,
        active_project=observer_context.active_project,
    )
    recent_intervention_feedback = await guardian_feedback_repository.summarize_recent_for_scope(
        scope=live_learning_resolution.dominant_scope,
        limit=5,
        session_id=session_id,
        active_project=observer_context.active_project,
    )
    advisory_learning_signal = live_learning_resolution.effective_signal
    effective_learning_signal = advisory_learning_signal
    try:
        procedural_guidance = await load_procedural_memory_guidance(
            normalized_intervention_type,
            continuity_thread_id=session_id,
            active_project=observer_context.active_project,
        )
        learning_arbitration = arbitrate_learning_signal(
            live_signal=advisory_learning_signal,
            procedural_guidance=procedural_guidance,
        )
        effective_learning_signal = learning_arbitration.effective_signal
    except Exception:
        logger.debug("Failed to load procedural guidance for guardian state", exc_info=True)
        learning_arbitration = arbitrate_learning_signal(
            live_signal=advisory_learning_signal,
            procedural_guidance=None,
        )
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
    retrieval = await plan_memory_retrieval(
        query=query,
        active_projects=active_projects,
    )
    memory_context = retrieval.semantic_context
    episodic_memory_context = retrieval.episodic_context
    memory_buckets = retrieval.memory_buckets
    memory_benchmark_diagnostics = _memory_benchmark_diagnostic_lines(retrieval.retrieval_diagnostics)
    memory_provider_diagnostics = _memory_provider_diagnostic_lines(retrieval.provider_diagnostics)
    try:
        memory_reconciliation_summary = await summarize_memory_reconciliation_state()
    except Exception:
        logger.debug("Failed to load memory reconciliation diagnostics", exc_info=True)
        memory_reconciliation_summary = {}
    memory_reconciliation_diagnostics = _memory_reconciliation_diagnostic_lines(
        memory_reconciliation_summary
    )
    try:
        snapshot_session_key = None
        if session_id is not None and session_record is not None:
            created_at = getattr(session_record, "created_at", None)
            if created_at is not None:
                snapshot_session_key = f"{session_id}:{created_at.isoformat()}"
            else:
                snapshot_session_key = session_id
        bounded_snapshot = await get_or_create_bounded_guardian_snapshot(
            soul_context=soul_context,
            session_id=snapshot_session_key,
        )
    except Exception:
        logger.debug("Failed to load bounded guardian snapshot", exc_info=True)
        bounded_snapshot, _ = await render_bounded_guardian_snapshot(
            soul_context=soul_context,
        )
    bounded_memory_context = _merge_memory_contexts(
        bounded_snapshot,
        _summarize_bounded_todos(
        todos=session_todos,
        ),
    )
    world_model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context=memory_context,
        current_session_history=current_session_history,
        recent_sessions_summary=recent_sessions_summary,
        recent_intervention_feedback=recent_intervention_feedback,
        active_projects=active_projects,
        recent_execution_summary=recent_execution_summary,
        memory_buckets=memory_buckets,
        learning_signal=effective_learning_signal,
    )
    world_model_status = _world_model_status(world_model)
    memory_status = (
        "degraded"
        if memory_requested and retrieval.degraded
        else _status_for_text(
            _merge_memory_contexts(memory_context, episodic_memory_context),
            requested=memory_requested,
        )
    )

    confidence = GuardianStateConfidence(
        overall=_overall_confidence(
            observer_confidence=observer_context.observer_confidence,
            world_model_status=world_model_status,
            memory_status=memory_status,
            current_session_status=_status_for_text(
                current_session_history,
                requested=session_id is not None,
            ),
            recent_sessions_status=_status_for_text(recent_sessions_summary),
        ),
        observer=observer_context.observer_confidence,
        world_model=world_model_status,
        memory=memory_status,
        current_session=_status_for_text(
            current_session_history,
            requested=session_id is not None,
        ),
        recent_sessions=_status_for_text(recent_sessions_summary),
    )
    intent_uncertainty_level, intent_resolution, intent_uncertainty_diagnostics = (
        _intent_uncertainty_diagnostics(
            user_message=user_message,
            observer_context=observer_context,
            world_model=world_model,
        )
    )
    judgment_proof_lines = _judgment_proof_lines(
        observer_context=observer_context,
        world_model=world_model,
        intent_uncertainty_diagnostics=intent_uncertainty_diagnostics,
    )

    return GuardianState(
        soul_context=soul_context,
        observer_context=observer_context,
        world_model=world_model,
        bounded_memory_context=bounded_memory_context,
        memory_context=memory_context,
        episodic_memory_context=episodic_memory_context,
        current_session_history=current_session_history,
        recent_sessions_summary=recent_sessions_summary,
        recent_intervention_feedback=recent_intervention_feedback,
        recent_execution_summary=recent_execution_summary,
        learning_guidance=_learning_guidance_text(
            delivery_bias=effective_learning_signal.bias,
            phrasing_bias=effective_learning_signal.phrasing_bias,
            cadence_bias=effective_learning_signal.cadence_bias,
            channel_bias=effective_learning_signal.channel_bias,
            escalation_bias=effective_learning_signal.escalation_bias,
            timing_bias=effective_learning_signal.timing_bias,
            blocked_state_bias=effective_learning_signal.blocked_state_bias,
            suppression_bias=effective_learning_signal.suppression_bias,
            thread_preference_bias=effective_learning_signal.thread_preference_bias,
        ),
        learning_diagnostics=_learning_diagnostics_lines(
            live_learning_resolution=live_learning_resolution,
            arbitration=learning_arbitration,
            active_project=observer_context.active_project,
        ),
        memory_benchmark_diagnostics=memory_benchmark_diagnostics,
        memory_provider_diagnostics=memory_provider_diagnostics,
        memory_reconciliation_diagnostics=memory_reconciliation_diagnostics,
        intent_uncertainty_level=intent_uncertainty_level,
        intent_resolution=intent_resolution,
        intent_uncertainty_diagnostics=intent_uncertainty_diagnostics,
        judgment_proof_lines=judgment_proof_lines,
        confidence=confidence,
    )
