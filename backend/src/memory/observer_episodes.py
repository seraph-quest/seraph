from __future__ import annotations

from src.db.models import MemoryEntityType, MemoryEpisodeType
from src.memory.repository import memory_repository
from src.observer.context import CurrentContext


_BLOCKED_STATES = {"deep_work", "in_meeting", "away"}


def _normalize(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _focus_signal(context: CurrentContext, *, active_project: str | None) -> str:
    if _normalize(context.current_event):
        return _normalize(context.current_event)
    if _normalize(context.active_goals_summary):
        return _normalize(context.active_goals_summary)
    if _normalize(active_project):
        return _normalize(active_project)
    if _normalize(context.active_window):
        return _normalize(context.active_window)
    return ""


def _activity_signal(context: CurrentContext) -> str:
    window = _normalize(context.active_window)
    user_state = _normalize(context.user_state)
    if window:
        return f"{user_state} in {window}" if user_state else window
    return user_state


async def _resolve_project_entity_id(
    *,
    active_project: str,
    old_project: str,
) -> str | None:
    candidate_names = tuple(
        dict.fromkeys(name for name in (active_project, old_project) if name)
    )
    if not candidate_names:
        return None

    resolved = await memory_repository.find_entities_by_names(
        names=candidate_names,
        entity_type=MemoryEntityType.project,
    )
    preferred_name = active_project or old_project
    entity = resolved.get(preferred_name)
    if entity is not None:
        return entity.id

    created = await memory_repository.get_or_create_entity(
        canonical_name=preferred_name,
        entity_type=MemoryEntityType.project,
    )
    return created.id


def should_record_activity_transition(old: CurrentContext, new: CurrentContext) -> bool:
    if old.user_state != new.user_state and (
        old.user_state in _BLOCKED_STATES or new.user_state in _BLOCKED_STATES
    ):
        return True
    return (
        old.active_window != new.active_window
        and bool(_normalize(new.active_window))
        and new.salience_level in {"medium", "high"}
    )


async def record_observer_transition_episodes(
    *,
    old_context: CurrentContext,
    new_context: CurrentContext,
    active_project: str | None,
) -> int:
    project_name = _normalize(active_project)
    old_project = _normalize(old_context.active_project)
    project_entity_id = await _resolve_project_entity_id(
        active_project=project_name,
        old_project=old_project,
    )

    episodes: list[dict[str, object]] = []

    if project_name != old_project and (project_name or old_project):
        summary = (
            f"Observer project shifted to {project_name}"
            if project_name
            else f"Observer project cleared from {old_project}"
        )
        episodes.append(
            {
                "episode_type": MemoryEpisodeType.observer,
                "summary": summary,
                "content": (
                    f"Observer project transition: "
                    f"{old_project or 'none'} -> {project_name or 'none'}."
                ),
                "project_entity_id": project_entity_id,
                "salience": 0.72,
                "confidence": 0.78,
                "metadata": {
                    "source": "observer_context_refresh",
                    "observer_transition": "project",
                    "previous_project": old_project or None,
                    "current_project": project_name or None,
                },
            }
        )

    old_focus = _focus_signal(old_context, active_project=old_context.active_project)
    new_focus = _focus_signal(new_context, active_project=project_name or new_context.active_project)
    if new_focus and new_focus != old_focus:
        episodes.append(
            {
                "episode_type": MemoryEpisodeType.observer,
                "summary": f"Observer focus shifted to {new_focus}",
                "content": f"Observer focus transition: {old_focus or 'none'} -> {new_focus}.",
                "project_entity_id": project_entity_id,
                "salience": 0.68,
                "confidence": 0.74,
                "metadata": {
                    "source": "observer_context_refresh",
                    "observer_transition": "focus",
                    "previous_focus": old_focus or None,
                    "current_focus": new_focus,
                },
            }
        )

    old_activity = _activity_signal(old_context)
    new_activity = _activity_signal(new_context)
    if new_activity and new_activity != old_activity and should_record_activity_transition(old_context, new_context):
        episodes.append(
            {
                "episode_type": MemoryEpisodeType.observer,
                "summary": f"Observer activity shifted to {new_activity}",
                "content": f"Observer activity transition: {old_activity or 'none'} -> {new_activity}.",
                "project_entity_id": project_entity_id,
                "salience": 0.64,
                "confidence": 0.72,
                "metadata": {
                    "source": "observer_context_refresh",
                    "observer_transition": "activity",
                    "previous_activity": old_activity or None,
                    "current_activity": new_activity,
                    "previous_user_state": old_context.user_state,
                    "current_user_state": new_context.user_state,
                },
            }
        )

    await memory_repository.create_episode_batch(items=episodes)
    return len(episodes)
