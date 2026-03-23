"""Delegation runtime tool backed by the existing specialist graph."""

from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Iterable

from smolagents import tool

from config.settings import settings

SPECIALIST_ALIASES: dict[str, str] = {
    "memory": "memory_keeper",
    "memories": "memory_keeper",
    "soul": "memory_keeper",
    "goals": "goal_planner",
    "goal": "goal_planner",
    "priorities": "goal_planner",
    "priority": "goal_planner",
    "research": "web_researcher",
    "web": "web_researcher",
    "search": "web_researcher",
    "files": "file_worker",
    "file": "file_worker",
    "code": "file_worker",
    "workflow": "workflow_runner",
    "workflows": "workflow_runner",
}

AUTO_ROUTE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workflow_runner", ("workflow", "runbook", "starter pack", "starter-pack")),
    ("memory_keeper", ("remember", "memory", "identity", "preference", "guardian record", "soul")),
    ("goal_planner", ("goal", "goals", "priority", "priorities", "plan", "roadmap")),
    ("web_researcher", ("research", "search", "look up", "lookup", "find", "web", "weather")),
    ("file_worker", ("file", "files", "workspace", "save", "write", "patch", "code", "script")),
)

_DELEGATION_DEPTH: ContextVar[int] = ContextVar("delegate_task_depth", default=0)


def _normalize_specialist_name(value: str) -> str:
    normalized = " ".join(value.strip().lower().replace("-", " ").split())
    if not normalized:
        return ""
    return SPECIALIST_ALIASES.get(normalized, normalized.replace(" ", "_"))


def _extract_output(result: object) -> str:
    output = getattr(result, "output", None)
    if isinstance(output, str) and output.strip():
        return output
    if isinstance(result, str):
        return result
    return str(result)


def _match_specialist(task: str, specialist_names: Iterable[str]) -> str | None:
    lowered = task.lower()
    available = set(specialist_names)
    for specialist_name, keywords in AUTO_ROUTE_KEYWORDS:
        if specialist_name not in available:
            continue
        if any(keyword in lowered for keyword in keywords):
            return specialist_name
    for specialist_name in available:
        if specialist_name.startswith("mcp_") and specialist_name.removeprefix("mcp_") in lowered:
            return specialist_name
    return None


@tool
def delegate_task(task: str, specialist: str = "") -> str:
    """Delegate a bounded subtask to a specialist runtime.

    Use this when a focused specialist can handle the subtask more cleanly than
    the current general agent. Prefer an explicit `specialist` value when you
    know which domain should handle the work.

    Args:
        task: The bounded subtask to hand off to a specialist.
        specialist: Optional specialist name or alias such as `research`,
            `files`, `goals`, `memory`, or `workflow`.

    Returns:
        The delegated specialist's final response, or a clear routing error.
    """
    from src.agent.specialists import build_all_specialists

    if not settings.use_delegation:
        return "Error: Delegation runtime is disabled."
    current_depth = _DELEGATION_DEPTH.get()
    if current_depth > 0:
        return "Error: Nested delegation is not allowed."

    specialists = build_all_specialists()
    if not specialists:
        return "Error: No specialists are currently available for delegation."

    specialists_by_name = {agent.name: agent for agent in specialists}
    specialist_name = _normalize_specialist_name(specialist)
    selected_name = specialist_name or _match_specialist(task, specialists_by_name.keys())
    if not selected_name:
        available = ", ".join(sorted(specialists_by_name))
        return (
            "Error: Unable to infer a specialist for this task. "
            f"Specify one of: {available}"
        )

    selected = specialists_by_name.get(selected_name)
    if selected is None:
        available = ", ".join(sorted(specialists_by_name))
        return f"Error: Unknown specialist '{specialist}'. Available specialists: {available}"

    token = _DELEGATION_DEPTH.set(current_depth + 1)
    try:
        result = selected.run(task, stream=False, reset=True)
        return _extract_output(result)
    finally:
        _DELEGATION_DEPTH.reset(token)
